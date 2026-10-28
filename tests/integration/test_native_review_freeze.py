"""Disposable native evidence freeze: real SQL permissions/locks, no live providers."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from uuid import UUID, uuid4

import httpx
import pytest

from app.jobs_v22.digest import canonical_json_bytes, verified_canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError, TransientJobError
from app.jobs_v22.models import utc_now
from app.report_v22.review_freeze_models import ReviewFreezeSelection
from app.report_v22.review_freeze_storage import ReviewFreezeRepository
from app.report_v22.review_projection import build_review_projection
from tests.integration.test_native_comparison_storage import prepared, native_database, inspect
from tests.integration.test_native_page_check_storage import page_setup, request as page_request, execute as page_execute, revise as revise_implementation
from tests.integration.test_native_ga4_collection_storage import ga4_setup, request as ga4_request, execute as ga4_execute

pytestmark = [pytest.mark.native_integration, pytest.mark.anyio, pytest.mark.timeout(70)]


@pytest.fixture
async def review_setup(prepared):
    p = prepared
    delivery = (await p["db"].query("select content from report_content_revisions where id=%s", [p["record"]["delivery_revision_id"]]))[0]["content"]
    p["review_choice"] = ReviewFreezeSelection.model_validate_json(json.dumps(dict(
        report=dict(id=p["record"]["report_id"], checksum=p["record"]["report_checksum"]),
        delivery=dict(id=p["record"]["delivery_revision_id"], revision=1, checksum=verified_request_digest(delivery)),
        implementation={k: p["record"][k] for k in ("id", "revision", "checksum")},
        actions=[dict(action_id=a["id"], page_current_id=None, page_previous_id=None, gsc_id=None, ga4_id=None) for a in delivery["actions"]])))
    async with httpx.AsyncClient(transport=httpx.MockTransport(p["db"].transport)) as client:
        p["reviews"] = ReviewFreezeRepository(url="https://local-test.invalid", service_role_key="local", http_client=client)
        yield p


async def freeze(p, *, review_id=None, selection=None):
    return await p["reviews"].freeze(**p["identity"], review_id=review_id or uuid4(), selection=selection or p["review_choice"])


def raw_args(p, resolved, *, body=None, review_id=None, selection=None):
    from app.report_v22.review_evidence_models import ReviewEvidenceCore
    b = body or resolved.bundle.model_dump(mode="json")
    core = {k: v for k, v in b.items() if k in ReviewEvidenceCore.model_fields}
    b = deepcopy(b)
    b["evidence_checksum"] = verified_request_digest(core)
    b["checksum"] = verified_request_digest({k: v for k, v in b.items() if k != "checksum"})
    return dict(p_user_id=str(p["identity"]["user_id"]), p_case_id=str(p["identity"]["case_id"]), p_id=str(review_id or uuid4()),
        p_selection=(selection or p["review_choice"]).model_dump(mode="json"), p_case_updated_at=resolved.case_updated_at,
        p_bundle_text=canonical_json_bytes(b).decode(), p_canonical_core=verified_canonical_json_bytes(core).decode(),
        p_canonical_bundle=verified_canonical_json_bytes({k: v for k, v in b.items() if k != "checksum"}).decode())


async def test_native_review_roundtrip_idempotence_and_no_charges(review_setup):
    p = review_setup; review_id = uuid4()
    values = await asyncio.gather(*(freeze(p, review_id=review_id) for _ in range(4)))
    assert all(v == values[0] for v in values)
    saved = values[0]
    assert saved.bundle.decisions == []
    projection = build_review_projection(saved.bundle)
    assert all(a.next_decision.decision_state == "needs_review" for a in projection.client_actions)
    assert await p["reviews"].read(**p["identity"], review_id=review_id) == saved
    assert await freeze(p, review_id=review_id) == saved
    assert len(await p["db"].query("select id from capability_review_evidence_bundles")) == 1
    assert await inspect(p["db"], p["scenario"]) == p["baseline"]


async def test_native_review_source_changes_and_permissions(review_setup):
    p = review_setup
    resolved = await p["reviews"].resolve(**p["identity"], selection=p["review_choice"])
    for field in ("report", "delivery", "implementation"):
        choice = p["review_choice"].model_dump(mode="json"); choice[field]["checksum"] = "sha256:" + "0" * 64
        with pytest.raises(DeterministicJobError):
            await freeze(p, selection=ReviewFreezeSelection.model_validate_json(json.dumps(choice)))
    await p["db"].query("update client_cases set updated_at=clock_timestamp() where id=%s", [p["identity"]["case_id"]])
    with pytest.raises(DeterministicJobError, match="STALE"):
        await p["reviews"].save(**p["identity"], review_id=uuid4(), selection=p["review_choice"], resolved=resolved)
    assert await p["db"].query("select id from capability_review_evidence_bundles") == []
    saved = await freeze(p)
    for identity in (dict(user_id=uuid4(), case_id=saved.case_id), dict(user_id=saved.user_id, case_id=uuid4())):
        with pytest.raises(DeterministicJobError, match="UNAVAILABLE"):
            await p["reviews"].read(**identity, review_id=saved.id)
    await p["db"].query("update client_cases set status='archived',archived_at=clock_timestamp() where id=%s", [saved.case_id])
    with pytest.raises(DeterministicJobError, match="UNAVAILABLE"): await freeze(p, review_id=saved.id)
    with pytest.raises(DeterministicJobError, match="UNAVAILABLE"):
        await p["reviews"].read(**p["identity"], review_id=saved.id)


async def test_native_review_tamper_canonical_and_limits(review_setup):
    p = review_setup; resolved = await p["reviews"].resolve(**p["identity"], selection=p["review_choice"])
    for change in ({"report_checksum":"sha256:"+"0"*64}, {"rules_version":"future"}, {"decisions":[{}]},
                   {"selections":[]}, {"extra":True}, {"user_id":str(uuid4())}, {"evaluated_at":"2099-01-01T00:00:00Z"}):
        args = raw_args(p, resolved, body=resolved.bundle.model_dump(mode="json") | change)
        with pytest.raises(Exception, match="REVIEW_(INVALID|STALE)"):
            await p["db"].rpc("freeze_v22_review_evidence", args)
    for field in ("p_canonical_core", "p_canonical_bundle"):
        args = raw_args(p, resolved); args[field] = " " + args[field]
        with pytest.raises(Exception, match="REVIEW_INVALID"): await p["db"].rpc("freeze_v22_review_evidence", args)
    args = raw_args(p, resolved)
    args["p_bundle_text"] = args["p_bundle_text"][:-1]+',"decisions":[]}'
    with pytest.raises(Exception, match="REVIEW_INVALID"): await p["db"].rpc("freeze_v22_review_evidence", args)
    args = raw_args(p, resolved); args["p_bundle_text"] = " " * 2_000_001
    with pytest.raises(Exception, match="REVIEW_TOO_LARGE"): await p["db"].rpc("freeze_v22_review_evidence", args)
    assert await p["db"].query("select id from capability_review_evidence_bundles") == []


async def test_native_review_roles_immutability_conflict_and_lost_ack(review_setup):
    p = review_setup; lost = False; review_id = uuid4(); original = p["reviews"]
    async def transport(req):
        nonlocal lost
        answer = await p["db"].transport(req)
        if req.url.path.endswith("/freeze_v22_review_evidence") and answer.status_code == 200 and not lost:
            lost = True
            raise httpx.ReadTimeout("private lost acknowledgement", request=req)
        return answer
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        p["reviews"] = ReviewFreezeRepository(url="https://local-test.invalid", service_role_key="local", http_client=client)
        with pytest.raises(TransientJobError): await freeze(p, review_id=review_id)
        saved = await freeze(p, review_id=review_id)
    p["reviews"] = original
    choice = p["review_choice"].model_dump(mode="json"); choice["actions"][0]["gsc_id"] = str(uuid4())
    with pytest.raises(DeterministicJobError, match="CONFLICT"):
        await freeze(p, review_id=review_id, selection=ReviewFreezeSelection.model_validate_json(json.dumps(choice)))
    for statement in ("update capability_review_evidence_bundles set id=id where id=%s", "delete from capability_review_evidence_bundles where id=%s"):
        with pytest.raises(Exception, match="IMMUTABLE"): await p["db"].query(statement, [saved.id])
        with pytest.raises(Exception, match="permission denied"): await p["db"].query(statement, [saved.id], True)
    rights = await p["db"].query("""select
      has_table_privilege('authenticated','capability_review_evidence_bundles','SELECT') a,
      has_table_privilege('service_role','capability_review_evidence_bundles','INSERT') b,
      has_function_privilege('anon','get_v22_review_evidence(uuid,uuid,uuid)','EXECUTE') c,
      has_function_privilege('authenticated','resolve_v22_review_evidence(uuid,uuid,jsonb,timestamptz)','EXECUTE') d,
      has_function_privilege('service_role','v22_review_hash(jsonb)','EXECUTE') e""")
    assert not any(rights[0].values())


async def test_native_review_canonical_parity(native_database):
    # Binary64 cutovers, UTF-16 ordering, escaping and legacy number transport.
    values = [dict(a=3.0, b=0.000001, c=1e-7, d=1e20, e=1e21, f=-0.0),
        {"\ue000":1, "\U00010000":2, "中":"\b\f\n\r\t\\\"/", "x":[True, False, None]},
        dict(x=1.2345678901234567, y=123456789.12345679)]
    for value in values:
        rows = await native_database.query("select v22_review_canonical(%s::jsonb) value", [canonical_json_bytes(value).decode()])
        assert rows[0]["value"] == verified_canonical_json_bytes(value).decode()


async def test_native_review_gsc_historical_versions_and_provider_revocation(review_setup):
    p = review_setup
    comparison = await p["repo"].freeze(**p["identity"], comparison_id=uuid4(), selection=p["selection"])
    choice = p["review_choice"].model_dump(mode="json"); choice["actions"][0]["gsc_id"] = str(comparison.id)
    choice = ReviewFreezeSelection.model_validate_json(json.dumps(choice))
    saved = await freeze(p, selection=choice)
    assert saved.bundle.selections[0].gsc == comparison
    await revise_implementation(p)
    await p["db"].query("update google_connections set granted_scopes='{}' where id=%s", [p["connection"]])
    another = await freeze(p, selection=choice)
    assert another.bundle.implementation.revision == 1
    assert another.bundle.selections[0].gsc == comparison
    assert await freeze(p, review_id=saved.id, selection=choice) == saved
    # Exact source content is checked again in SQL, even if the caller re-signs a replacement.
    resolved = await p["reviews"].resolve(**p["identity"], selection=choice)
    b = resolved.bundle.model_dump(mode="json"); b["selections"][0]["gsc"]["result"]["metrics"] = []
    with pytest.raises(Exception, match="REVIEW_INVALID"):
        await p["db"].rpc("freeze_v22_review_evidence", raw_args(p, resolved, body=b, selection=choice))
    # Numerically equal JSONB must not erase a legacy snapshot's 3.0/3 distinction.
    b = resolved.bundle.model_dump(mode="json")
    totals = b["selections"][0]["gsc"]["request"]["before"]["normalized_payload"]["current"]["totals"]["rows"][0]
    assert isinstance(totals["position"], float) and totals["position"].is_integer()
    totals["position"] = int(totals["position"])
    with pytest.raises(Exception, match="REVIEW_INVALID"):
        await p["db"].rpc("freeze_v22_review_evidence", raw_args(p, resolved, body=b, selection=choice))
    assert await inspect(p["db"], p["scenario"]) == p["baseline"]


@pytest.mark.parametrize("unable", [False, True])
async def test_native_review_page_observations_not_pending_tasks(review_setup, page_setup, unable):
    p = review_setup; job = await page_request(p)
    choice = p["review_choice"].model_dump(mode="json"); choice["actions"][0]["page_current_id"] = str(job.id)
    choice = ReviewFreezeSelection.model_validate_json(json.dumps(choice))
    with pytest.raises(DeterministicJobError): await freeze(p, selection=choice)
    if unable:
        def handler(req): return httpx.Response(404)
    else: handler = None
    job = await page_execute(p, job, handler)
    call_count = len(p["calls"])
    saved = await freeze(p, selection=choice)
    assert saved.bundle.selections[0].page.current == job.observation
    assert saved.bundle.selections[0].page.current.status == ("unable_to_check" if unable else "checked")
    assert len(p["calls"]) == call_count
    await revise_implementation(p)
    assert (await freeze(p, selection=choice)).bundle.selections[0].page.current == job.observation


@pytest.mark.parametrize("restricted", [False, True])
async def test_native_review_ga4_saved_counts_not_new_collection(review_setup, ga4_setup, restricted):
    p = review_setup; job = await ga4_request(p)
    choice = p["review_choice"].model_dump(mode="json"); choice["actions"][0]["ga4_id"] = str(job.id)
    choice = ReviewFreezeSelection.model_validate_json(json.dumps(choice))
    with pytest.raises(DeterministicJobError): await freeze(p, selection=choice)
    original = p["provider_handler"]
    def handler(req):
        raw = original(req).json()
        if restricted and req.method == "POST": raw["metadata"]["subjectToThresholding"] = True
        return httpx.Response(200, json=raw)
    job = await ga4_execute(p, job, handler=handler)
    call_count = len(p["calls"])
    await p["db"].query("update google_connections set granted_scopes='{}' where id=%s", [p["connection"]])
    saved = await freeze(p, selection=choice)
    assert saved.bundle.selections[0].ga4 == job
    facts = build_review_projection(saved.bundle).client_actions[0].facts.ga4
    assert facts.state == ("withheld" if restricted else "available")
    assert len(p["calls"]) == call_count
    assert await inspect(p["db"], p["scenario"]) == p["baseline"]


async def test_native_review_freeze_waits_for_case_change_then_rejects(review_setup):
    from psycopg import AsyncConnection
    p = review_setup; resolved = await p["reviews"].resolve(**p["identity"], selection=p["review_choice"])
    async with await AsyncConnection.connect(p["db"].url) as connection:
        await connection.execute("update client_cases set status='archived',archived_at=clock_timestamp() where id=%s", [p["identity"]["case_id"]])
        saving = asyncio.create_task(p["reviews"].save(**p["identity"], review_id=uuid4(), selection=p["review_choice"], resolved=resolved))
        await asyncio.sleep(.05)
        assert not saving.done()
        await connection.commit()
        with pytest.raises(DeterministicJobError, match="UNAVAILABLE"): await asyncio.wait_for(saving, 5)
    assert await p["db"].query("select id from capability_review_evidence_bundles") == []


async def test_native_review_exact_revision_beyond_history_limit(review_setup):
    p = review_setup
    for revision in range(2, 23):
        record = deepcopy(p["record"]); record.update(revision=revision, recorded_at=utc_now().isoformat().replace("+00:00", "Z"))
        record["entries"][0]["note"] = f"Correction {revision}"
        body = {k: v for k, v in record.items() if k != "checksum"}; record["checksum"] = verified_request_digest(body)
        await p["db"].query("select save_v22_implementation_record(%s,%s,%s,%s,%s,%s::jsonb,%s)",
            [p["identity"]["user_id"], p["identity"]["case_id"], record["report_id"], record["delivery_revision_id"], revision-1,
                json.dumps(record), verified_canonical_json_bytes(body).decode()], True)
    history = await p["db"].query("select revision from get_v22_implementation_records(%s,%s,%s,%s)",
        [p["identity"]["user_id"], p["identity"]["case_id"], p["record"]["report_id"], p["record"]["delivery_revision_id"]], True)
    assert len(history) == 20 and all(r["revision"] > 1 for r in history)
    assert (await freeze(p)).bundle.implementation.revision == 1


async def test_native_review_source_missing_and_collision_are_private(review_setup):
    p = review_setup; saved = await freeze(p)
    choice = p["review_choice"].model_dump(mode="json")
    for field in ("page_current_id", "gsc_id", "ga4_id"):
        candidate = deepcopy(choice); candidate["actions"][0][field] = str(uuid4())
        with pytest.raises(DeterministicJobError):
            await freeze(p, selection=ReviewFreezeSelection.model_validate_json(json.dumps(candidate)))
    other_case = uuid4()
    await p["db"].query("""insert into client_cases(id,user_id,site_url,normalized_domain,business_name,business_identity,operating_model,primary_service,target_market)
      select %s,user_id,site_url,normalized_domain,business_name,business_identity-'primary_location',operating_model,primary_service,target_market
      from client_cases where id=%s""", [other_case, saved.case_id])
    assert await p["reviews"].read(user_id=saved.user_id, case_id=other_case, review_id=saved.id) is None
    args = raw_args(p, await p["reviews"].resolve(**p["identity"], selection=p["review_choice"]), review_id=saved.id)
    args["p_case_id"] = str(other_case)
    with pytest.raises(Exception, match="REVIEW_UNAVAILABLE"): await p["db"].rpc("freeze_v22_review_evidence", args)
    assert len(await p["db"].query("select id from capability_review_evidence_bundles")) == 1


async def test_native_review_time_window_and_replay_keep_original_time(review_setup):
    p = review_setup; resolved = await p["reviews"].resolve(**p["identity"], selection=p["review_choice"])
    b = resolved.bundle.model_dump(mode="json")
    for delta in (-600, 120):
        b["evaluated_at"] = (utc_now()+timedelta(seconds=delta)).isoformat().replace("+00:00", "Z")
        with pytest.raises(Exception, match="REVIEW_STALE"):
            await p["db"].rpc("freeze_v22_review_evidence", raw_args(p, resolved, body=b))
    b["evaluated_at"] = (utc_now()+timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    args = raw_args(p, resolved, body=b)
    saved = await p["db"].rpc("freeze_v22_review_evidence", args)
    value = p["reviews"].checked(canonical_json_bytes(saved), **p["identity"], review_id=UUID(args["p_id"]))
    # Recovery bypasses fresh-input time checks, but still requires current ownership and exact selection.
    args.update(p_case_updated_at="2000-01-01T00:00:00Z", p_bundle_text="{}", p_canonical_core="{}", p_canonical_bundle="{}")
    assert await p["db"].rpc("freeze_v22_review_evidence", args) == saved
    assert (await freeze(p, review_id=value.id)).frozen_at == value.frozen_at
