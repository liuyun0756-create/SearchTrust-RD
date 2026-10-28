"""Real disposable PostgreSQL/Redis/ARQ; all website and DNS responses are mocked."""
import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest
from arq.worker import Worker, func

from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.page_check_entry import PageCheckCommand, PageCheckEntry
from app.report_v22.page_check_limits import PageCheckRateLimited
from app.report_v22.page_check_worker import reconcile_v22_page_checks, execute_v22_page_check
from tests.integration.test_native_page_check_storage import page_setup, prepared, native_database, request, raw_request, execute, expire_fixture, unaffected, inspect, revise

pytestmark = [pytest.mark.native_integration, pytest.mark.anyio, pytest.mark.timeout(60)]


def command(p, **changes):
    return PageCheckCommand(**(dict(operation="context", **p["identity"], report_id=UUID(p["record"]["report_id"]),
        delivery_revision_id=UUID(p["record"]["delivery_revision_id"]), check_id=None, selection=None) | changes))


async def context(p):
    return await PageCheckEntry(p["pages"]).run(command(p))


async def seed_terminal_quota(p, count):
    """Admin-only history fixture. Production role cannot bypass any guard."""
    job = await request(p); claimed = await p["pages"].claim_check(job.id); await p["pages"].fail_check(claimed)
    await p["db"].query("alter table capability_page_check_jobs disable trigger immutable_page_check")
    await p["db"].query("alter table capability_page_check_jobs disable trigger page_check_rate")
    try:
        await p["db"].query("update capability_page_check_jobs set created_at=clock_timestamp()-interval '2 hours' where id=%s", [job.id])
        for n in range(count - 1):
            await p["db"].query("""insert into capability_page_check_jobs(id,user_id,case_id,report_id,delivery_revision_id,
              case_updated_at,selection,plan,status,error_code,created_at,completed_at)
              select %s,user_id,case_id,report_id,delivery_revision_id,case_updated_at,selection,plan,'failed','PAGE_CHECK_EXPIRED',
                clock_timestamp()-(%s*interval '1 minute'),clock_timestamp() from capability_page_check_jobs where id=%s""",
              [uuid4(), 10 + n * 5, job.id])
    finally:
        await p["db"].query("alter table capability_page_check_jobs enable trigger immutable_page_check")
        await p["db"].query("alter table capability_page_check_jobs enable trigger page_check_rate")


async def test_native_page_owner_entry_dispatch_and_no_effects(page_setup, arq_pool, redis_prefix, monkeypatch):
    p = page_setup; entry = PageCheckEntry(p["pages"])
    initial = await context(p)
    assert initial.eligibility == "eligible" and initial.limits.used == 0 and initial.history == []
    assert p["calls"] == []
    ask = command(p, operation="request", check_id=uuid4(), selection=p["choice"])
    first = await entry.run(ask)
    assert first.status == "queued" and await entry.run(ask) == first
    from app.report_v22 import page_check_executor
    original = page_check_executor.collect_page_implementation
    async def controlled(plan, **kwargs):
        return await original(plan, observation_id=kwargs["observation_id"], resolver=p["resolver"],
            client_factory=lambda target: httpx.AsyncClient(transport=httpx.MockTransport(p["handler"])))
    monkeypatch.setattr(page_check_executor, "collect_page_implementation", controlled)
    ctx = dict(redis=arq_pool, page_check_repository=p["pages"], page_check_queue=f"{redis_prefix}:page")
    await reconcile_v22_page_checks(ctx); await reconcile_v22_page_checks(ctx)
    worker = Worker([func(execute_v22_page_check, max_tries=1, keep_result=0)], redis_pool=arq_pool,
        queue_name=ctx["page_check_queue"], ctx=ctx, burst=True, handle_signals=False, poll_delay=.01)
    try: await worker.async_run()
    finally: await worker.close()
    done = await entry.run(ask)
    assert done.status == "completed" and done.observation.status == "checked" and len(p["calls"]) == 2
    assert (await context(p)).limits.used == 1
    await execute_v22_page_check(ctx, str(first.id))
    assert len(p["calls"]) == 2
    assert await unaffected(p) == p["ordinary"] and await inspect(p["db"], p["scenario"]) == p["baseline"]


@pytest.mark.parametrize("status", ["queued", "failed", "checked", "unable", "expired"])
async def test_native_accepted_requests_count_in_every_state(page_setup, status):
    p = page_setup; job = await request(p)
    if status == "failed": await p["pages"].fail_check(await p["pages"].claim_check(job.id))
    if status in ("checked", "unable"):
        await execute(p, job, (lambda req: httpx.Response(403)) if status == "unable" else None)
    if status == "expired": await expire_fixture(p, job); await p["pages"].expire_check(job.id)
    value = await context(p)
    assert value.limits.used == 1
    if status != "expired":
        with pytest.raises(PageCheckRateLimited) as error: await request(p)
        assert error.value.limits.reason == ("active" if status == "queued" else "cooldown")
    assert len(value.history) == 1


async def test_native_quota_case_wide_concurrent_last_slot_and_private_helpers(page_setup):
    p = page_setup; await seed_terminal_quota(p, 9)
    # Different URL/conditions do not reset the same Case allowance.
    other = p["choice"].model_copy(update={"target_url": p["choice"].target_url + "-other"})
    results = await asyncio.gather(request(p), request(p, selection=other), return_exceptions=True)
    accepted = [r for r in results if not isinstance(r, Exception)]
    assert len(accepted) == 1 and sum(isinstance(r, PageCheckRateLimited) for r in results) == 1
    job = accepted[0]; await p["pages"].fail_check(await p["pages"].claim_check(job.id))
    with pytest.raises(PageCheckRateLimited) as error: await request(p)
    assert error.value.limits.used == 10 and error.value.limits.reason == "quota"
    assert (await request(p, check_id=job.id, selection=job.selection)).status == "failed"
    value = await context(p)
    assert len(value.history) == 10 and value.limits.reason == "quota" and not p["calls"]
    # Wrong source is rejected before quota; a rejected insert does not count.
    with pytest.raises(DeterministicJobError) as bad:
        await request(p, selection=other.model_copy(update={"action_id": "not-an-action"}))
    assert not isinstance(bad.value, PageCheckRateLimited) and (await context(p)).limits.used == 10
    for role in ("service_role", "authenticated", "anon"):
        rights = await p["db"].query("select has_function_privilege(%s,'v22_page_check_limits(uuid,timestamptz)','EXECUTE') allowed", [role])
        assert not rights[0]["allowed"]


async def test_native_exact_cooldown_and_rolling_window_boundaries(page_setup):
    p = page_setup; job = await request(p); await p["pages"].fail_check(await p["pages"].claim_check(job.id))
    # Exact instants exercise the same private calculation used by the trigger;
    # the public API has no clock parameter and the service role cannot call it.
    values = await p["db"].query("""select
      v22_page_check_limits(case_id,created_at+interval '5 minutes'-interval '1 microsecond') before,
      v22_page_check_limits(case_id,created_at+interval '5 minutes') boundary,
      v22_page_check_limits(case_id,created_at+interval '24 hours'-interval '1 microsecond') last,
      v22_page_check_limits(case_id,created_at+interval '24 hours') expired
      from capability_page_check_jobs where id=%s""", [job.id])
    v = values[0]
    assert v["before"]["reason"] == "cooldown" and v["boundary"]["reason"] is None
    assert v["last"]["used"] == 1 and v["expired"]["used"] == 0


@pytest.mark.parametrize("state", ["queued", "running"])
async def test_native_read_does_not_expire_but_background_cleanup_does(page_setup, state):
    p = page_setup; job = await request(p)
    if state == "running": await p["pages"].claim_check(job.id)
    await expire_fixture(p, job)
    assert (await context(p)).history[0].status == state
    assert await p["db"].rpc("prepare_v22_page_check_dispatch", {}) == []
    assert (await context(p)).history[0].error_code == ("PAGE_CHECK_EXPIRED" if state == "queued" else "PAGE_CHECK_INTERRUPTED") and not p["calls"]


async def test_native_quota_expires_at_tenth_newest_boundary(page_setup):
    p = page_setup; await seed_terminal_quota(p, 10)
    limits = (await context(p)).limits
    assert limits.reason == "quota"
    row = (await p["db"].query("""select
      v22_page_check_limits(%s,%s::timestamptz-interval '1 microsecond') before,
      v22_page_check_limits(%s,%s::timestamptz) boundary""",
      [p["identity"]["case_id"], limits.next_request_at, p["identity"]["case_id"], limits.next_request_at]))[0]
    assert row["before"]["used"] == 10 and row["before"]["reason"] == "quota"
    assert row["boundary"]["used"] == 9 and row["boundary"]["reason"] is None


async def test_native_new_delivery_and_action_share_the_case_allowance(page_setup):
    from copy import deepcopy
    from app.jobs_v22.models import utc_now
    from app.report_v22.capability import ImplementationReference
    from tests.integration.test_native_page_check_storage import signed
    p = page_setup; first = await request(p); await p["pages"].fail_check(await p["pages"].claim_check(first.id))
    delivery = (await p["db"].query("select content from report_content_revisions where id=%s", [first.delivery_revision_id]))[0]["content"]
    delivery.update(expected_revision=1, client_note="Second approved delivery")
    approved = (await p["db"].query("select * from save_v22_completed_report_revision(%s,%s,%s,%s::jsonb)",
        [first.user_id, first.case_id, first.report_id, json.dumps(delivery)], True))[0]
    record = deepcopy(p["record"])
    record.update(id=str(uuid4()), delivery_revision_id=str(approved["id"]), delivery_revision=2,
        recorded_at=utc_now().isoformat().replace("+00:00", "Z"))
    record["entries"][0]["action_id"] = delivery["actions"][-1]["id"]
    record, canonical = signed(record)
    await p["db"].query("select save_v22_implementation_record(%s,%s,%s,%s,0,%s::jsonb,%s)",
        [first.user_id, first.case_id, first.report_id, approved["id"], json.dumps(record), canonical], True)
    choice = p["choice"].model_copy(update={"implementation": ImplementationReference.model_validate_json(json.dumps({k: record[k] for k in ("id", "revision", "checksum")})),
        "action_id": record["entries"][0]["action_id"]})
    entry = PageCheckEntry(p["pages"])
    other = command(p, delivery_revision_id=approved["id"])
    value = await entry.run(other)
    assert value.history == [] and value.limits.used == 1 and value.eligibility == "eligible"
    with pytest.raises(PageCheckRateLimited):
        await entry.run(other.model_copy(update={"operation": "request", "check_id": uuid4(), "selection": choice}))
    # Using the second delivery's selection in the first route is rejected BEFORE creation.
    with pytest.raises(DeterministicJobError) as mismatch:
        await entry.run(command(p, operation="request", check_id=uuid4(), selection=choice))
    assert not isinstance(mismatch.value, PageCheckRateLimited)
    assert len((await context(p)).history) == 1 and not p["calls"]


async def test_native_changed_business_keeps_authorized_history(page_setup):
    p = page_setup; first = await request(p); await execute(p, first)
    await p["db"].query("""insert into case_business_scope_versions(id,user_id,case_id,revision,scope)
      select %s,user_id,case_id,revision+1,scope from case_business_scope_versions where case_id=%s order by revision desc limit 1""",
      [uuid4(), first.case_id])
    current = await context(p)
    assert current.eligibility == "source_changed" and len(current.history) == 1
    entry = PageCheckEntry(p["pages"])
    saved = await entry.run(command(p, operation="read", check_id=first.id))
    assert saved.observation.status == "checked"
    with pytest.raises(DeterministicJobError):
        await entry.run(command(p, operation="request", check_id=uuid4(), selection=p["choice"]))
    assert (await context(p)).limits.used == 1


@pytest.mark.parametrize("field", ["user_id", "report_id", "delivery_revision_id"])
async def test_native_wrong_route_never_creates_check(page_setup, field):
    p = page_setup; entry = PageCheckEntry(p["pages"])
    with pytest.raises(DeterministicJobError):
        await entry.run(command(p, operation="request", check_id=uuid4(), selection=p["choice"], **{field: uuid4()}))
    assert (await context(p)).limits.used == 0 and not p["calls"]


async def test_native_previous_revision_history_and_request_recovery_survive_revision(page_setup):
    p = page_setup; entry = PageCheckEntry(p["pages"])
    ask = command(p, operation="request", check_id=uuid4(), selection=p["choice"])
    job = await request(p, check_id=ask.check_id); await execute(p, job); await revise(p)
    value = await context(p)
    assert value.implementation.revision == 2 and value.history[0].id == job.id
    assert (await entry.run(ask)).selection.implementation.revision == 1
    with pytest.raises(DeterministicJobError): await entry.run(ask.model_copy(update={"check_id": uuid4()}))
    assert (await context(p)).limits.used == 1


async def test_native_rolled_back_creation_does_not_consume_quota(page_setup):
    from psycopg import AsyncConnection
    p = page_setup
    plan, marker, business = await p["pages"].resolve(**p["identity"], selection=p["choice"])
    from app.jobs_v22.digest import verified_canonical_json_bytes
    async with await AsyncConnection.connect(p["db"].url) as connection:
        await connection.execute("set local role service_role")
        await connection.execute("select request_v22_page_check(%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",
            [p["identity"]["user_id"], p["identity"]["case_id"], uuid4(), p["choice"].model_dump_json(), plan.model_dump_json(), marker,
             verified_canonical_json_bytes(plan.model_dump(mode="json", exclude={"checksum"})).decode(), business])
        await connection.rollback()
    assert (await context(p)).limits.used == 0
    assert (await request(p)).status == "queued"
