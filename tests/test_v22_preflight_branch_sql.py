"""Run acquisition before a Case has any report, using the actual migrated SQL."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from fakeredis.aioredis import FakeRedis
import pytest

from app.core.config import settings
from app.jobs_v22.capability_acquisition_intake import submit_acquisition
from app.jobs_v22.capability_acquisition_preparation import CapabilityAcquisitionPreparer
from app.jobs_v22.capability_acquisition_worker import LeasedPreparationRpc
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.capability_public_stage import CheckpointedCapabilityPublicStage
from app.jobs_v22.capability_result_persistence import SupabaseCapabilityResultPersister
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.cost_models import ALLOWED_COUNTER_KEYS
from app.jobs_v22.digest import request_digest
from test_p0_08_gold import deny_network
from test_p1_01_sql_preparation import LocalSql, SqlError, compressed_collection
from test_v22_capability_acquisition_preparation import inputs


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_first_prospect_uses_immutable_preflight_pin_through_store(monkeypatch):
    workspace = Path(os.environ.get("SEARCHTRUST_SQL_WORKSPACE", Path(__file__).resolve().parents[2] / "search-trust"))
    if not shutil.which("node") or not (workspace / "node_modules/@electric-sql/pglite").exists():
        pytest.skip("Real SQL replay requires Node, the frontend migration workspace, and PGlite.")
    if not (workspace / "supabase/migrations/20261123100000_pin_preflight_gbp_branch.sql").is_file():
        pytest.skip("The matching frontend GBP branch migration is required.")

    monkeypatch.setattr(settings, "V22_TARGET_KEYWORDS_ENABLED", False)
    deny_network(monkeypatch)
    args, envelope, _ = inputs()
    req = envelope.analyze_request
    owner, job, workflow = args["user_id"], args["job_id"], uuid4()
    case = req.case_id
    found = envelope.competitor_discovery
    now = datetime.now(timezone.utc)
    profile = args["collected"].customer_public_gbp.snapshot.record
    evidence = dict(preflight_id=str(uuid4()), observed_at=(now-timedelta(minutes=1)).isoformat(),
        expires_at=(now+timedelta(minutes=15)).isoformat(), branch_ref="12345",
        public_gbp_url=str(req.business_identity.public_gbp_url), domain=req.business_identity.normalized_domain,
        entity_keys={key.kind:key.value for key in profile.observed_entity_keys}, gbp_website_url=str(profile.fields.website_url.value),
        gbp_phone=profile.fields.phone.value, gbp_address=profile.fields.address.value, country_code=req.target_market.country_code,
        phone_matched=False, mode_material=[], mode_evidence_complete=True)
    bridge = LocalSql(workspace, Path(__file__).parent / "support/a3_sql_replay.mjs")
    redis = FakeRedis(decode_responses=False)
    try:
        bridge.command(dict(op="init", seed=dict(owner=str(owner), caseId=str(case),
            identity=req.business_identity.model_dump(mode="json"), service=req.primary_service,
            market=req.target_market.model_dump(mode="json"))))
        save = dict(p_user_id=str(owner), p_case_id=str(case), p_request_id=str(uuid4()), p_evidence=evidence,
            p_reason="Verified branch using the saved website evidence", p_operating_mode=None, p_customers_can_visit=False)
        receipt = await bridge.post("save_v22_preflight_branch_confirmation", save)
        assert receipt == await bridge.post("save_v22_preflight_branch_confirmation", save)
        reserve = dict(p_user_id=str(owner), p_case_id=str(case), p_workflow_id=str(workflow), p_discovery_job_id=str(found.discovery_id),
            p_workflow_idempotency_key="sql-branch-workflow", p_task_idempotency_key="sql-branch-discovery",
            p_confirmation_id=receipt["id"], p_public_gbp_url=evidence["public_gbp_url"], p_planning_revision=None,
            p_queries_canonical=None, p_keyword_scope=None)
        await bridge.post("start_v22_prospect_discovery_with_branch", reserve)
        later = await bridge.post("save_v22_preflight_branch_confirmation", {**save, "p_request_id":str(uuid4())})
        assert later["revision"] == 2
        with pytest.raises(SqlError, match="GBP_BRANCH_CONFLICT"):
            await bridge.post("start_v22_prospect_discovery_with_branch", {**reserve, "p_confirmation_id":later["id"]})
        reservation = await bridge.post("start_v22_prospect_discovery_with_branch", reserve)
        assert reservation["idempotent"] and reservation["credit_balance"] == 2
        verify = SimpleNamespace(verify=AsyncMock(return_value=found))
        repo = SupabaseCapabilityInputRepository(url="https://unused.example",service_role_key="local-test",http_client=None)
        repo.rpc = bridge
        status = await submit_acquisition(repository=repo, verifier=verify, queue=SimpleNamespace(enqueue=AsyncMock()),
            user_id=owner, job_id=job, workflow_id=workflow, discovery_id=found.discovery_id,
            idempotency_key="sql-branch-analysis", previous_job_id=None, request=req, now=datetime.now(timezone.utc),
            planning_revision=None)
        assert status.job_id == job
        token = uuid4()
        claim = await bridge.post("claim_v22_acquisition_preparation", dict(p_job_id=str(job), p_run_generation=1,
            p_lease_token=str(token), p_max_attempts=3))
        prep = claim["preparation"]
        created = datetime.fromisoformat(prep["created_at"])
        collected = compressed_collection(args["collected"], old_start=args["created_at"], old_end=args["now"], created=created)

        async def costs():
            return {**dict.fromkeys(ALLOWED_COUNTER_KEYS,0), "cost_schema_version":1, "cost_ledger_revision":3}

        repo.rpc = LeasedPreparationRpc(bridge, token=token, before_freeze=costs)
        dispatch = await CapabilityAcquisitionPreparer(repository=repo, collector=SimpleNamespace(collect_sources=AsyncMock(return_value=collected)),
            discovery_verifier=verify).prepare(user_id=owner, job_id=job, idempotency_key="sql-branch-analysis",
            business_version_id=UUID(prep["business"]["id"]), request=envelope, checkpoints=SimpleNamespace(run_generation=1),
            cost_ledger=SimpleNamespace(job_id=job,job_created_at=created))
        frozen = await repo.resolve(job_id=job,request=dispatch.request.capability_request,run_generation=1)
        proof = frozen.manifest.identity_inheritance
        assert proof.source_identity_version_id == UUID(receipt["id"])
        assert (proof.branch, proof.operating_model, proof.visits) == ("retained", "needs_confirmation", "not_previously_confirmed")
        for source in frozen.request.evidence_input.sources:
            assert request_digest(source.payload) == source.binding.payload_checksum
        report = await CheckpointedCapabilityPublicStage().build(job_id=job, manifest=frozen.manifest, request=frozen.request,
            checkpoints=JobCheckpoints(redis,prefix="local-preflight-branch",ttl_seconds=600))
        persister = SupabaseCapabilityResultPersister(url="https://unused.example",service_role_key="local-test",http_client=None)
        persister.rpc = bridge
        completion = await persister.persist(user_id=owner,case_id=case,job_id=job,run_generation=1,manifest=frozen.manifest,report=report)
        assert completion.report_id == job and report.schema_version == "2.2.16"
    finally:
        await redis.aclose()
        bridge.close()
